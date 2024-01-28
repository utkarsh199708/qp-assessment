package com.grocery.booking.grocery.Repository;

import com.grocery.booking.grocery.Entity.GroceryItem;
import org.springframework.data.jpa.repository.JpaRepository;
import org.springframework.data.jpa.repository.Modifying;
import org.springframework.data.jpa.repository.Query;
import org.springframework.data.repository.query.Param;

import java.util.List;

public interface GroceryItemRepository extends JpaRepository<GroceryItem, Long> {

    List<GroceryItem> findByQuantityGreaterThan(int quantity);

    // New method to update the inventory level of a grocery item
    @Modifying
    @Query("UPDATE GroceryItem g SET g.quantity = g.quantity - :quantity WHERE g.id = :itemId")
    int updateInventoryLevel(@Param("itemId") Long itemId, @Param("quantity") int quantity);
}
